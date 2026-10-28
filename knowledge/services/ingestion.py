"""Dispatch document ingestion either synchronously or in the background.

Controlled by ``INGESTION_MODE``:

* ``sync``   - run in-request (default). Returns the ``IngestionResult``.
* ``thread`` - run in a daemon thread and return ``None`` immediately, so large
  uploads don't time out the request. The document's ``processing_status``
  reflects progress (``pending`` -> ``processing`` -> ``completed``/``failed``).
* ``celery`` - enqueue a Celery task (production). Returns ``None`` immediately;
  a worker does the parse/chunk/embed. Requires Redis + a running worker.

The thread mode is a zero-infrastructure option suited to a single server;
``celery`` is the multi-worker / production option. The call sites never change.
"""

from __future__ import annotations

import logging
import threading

from django.conf import settings

from ..models import UploadedDocument
from .chunking import IngestionResult, ingest_document

logger = logging.getLogger(__name__)


def _run_in_thread(document_id) -> None:
    from django.db import connection

    try:
        document = UploadedDocument.objects.get(pk=document_id)
        ingest_document(document)
    except UploadedDocument.DoesNotExist:
        logger.error("Background ingestion: document %s no longer exists", document_id)
    except Exception:
        # ingest_document already persists FAILED status + error_message.
        logger.exception("Background ingestion failed for %s", document_id)
    finally:
        # Each thread gets its own DB connection; close it so it isn't leaked.
        connection.close()


def dispatch_ingestion(document: UploadedDocument) -> IngestionResult | None:
    """Ingest ``document`` per ``INGESTION_MODE``.

    Returns the ``IngestionResult`` in sync mode, or ``None`` when the work was
    handed off to the background (caller should treat the document as pending).
    """
    mode = getattr(settings, "INGESTION_MODE", "sync")
    if mode == "celery":
        from ..tasks import ingest_document_task

        ingest_document_task.delay(str(document.pk))
        logger.info("Enqueued Celery ingestion for %s", document.pk)
        return None
    if mode == "thread":
        threading.Thread(target=_run_in_thread, args=(document.pk,), daemon=True).start()
        logger.info("Queued background ingestion for %s", document.pk)
        return None
    return ingest_document(document)
