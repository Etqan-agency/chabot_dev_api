"""Run one API-content sync, independently of who asked for it.

The pipeline -- fetch every page, census the payload, structure, analyse,
validate, embed -- is the same whether it runs inside the request or on a Celery
worker. It lives here so neither caller owns it: the view maps the outcome onto
HTTP status codes, the task maps it onto log lines, and both get identical
behaviour and identical ``APIIngestionRun`` bookkeeping.

Every exit path is recorded on the run before it leaves this module. A caller
that catches one of these exceptions is therefore free to report it however it
likes without worrying that the run row was left dangling mid-stage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from .api_content_processor import (
    APIContentProcessingError,
    APIContentRAGProcessor,
    IngestionBlocked,
    extract_all,
    source_statistics,
)
from .api_fetch import fetch_all_pages

logger = logging.getLogger(__name__)

_FALSEY = {"0", "false", "no", "off"}
_TRUTHY = {"1", "true", "yes", "on"}


class EmptySource(Exception):
    """The endpoint answered, but carried nothing importable."""


@dataclass
class SyncParams:
    """The request body, already coerced -- so the worker never re-parses it.

    Celery serialises this to JSON, so every field stays a primitive.
    """

    api_url: str
    document_name: str = "API Content"
    items_key: str = "results"
    ai_analysis: bool = True
    full_refresh: bool = False
    max_pages: int | None = None

    @classmethod
    def from_request_data(cls, data: dict) -> SyncParams:
        try:
            max_pages = int(data.get("max_pages") or 0) or None
        except (TypeError, ValueError):
            max_pages = None
        return cls(
            api_url=data.get("api_url") or "",
            document_name=data.get("document_name") or "API Content",
            items_key=data.get("items_key") or "results",
            ai_analysis=str(data.get("ai_analysis", "true")).lower() not in _FALSEY,
            full_refresh=str(data.get("full_refresh", "")).lower() in _TRUTHY,
            max_pages=max_pages,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_url": self.api_url,
            "document_name": self.document_name,
            "items_key": self.items_key,
            "ai_analysis": self.ai_analysis,
            "full_refresh": self.full_refresh,
            "max_pages": self.max_pages,
        }


@dataclass
class SyncResult:
    """What a completed sync produced -- the body the API reports verbatim."""

    pages_fetched: int = 0
    truncated: bool = False
    stats: dict = field(default_factory=dict)
    source_stats: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages_fetched": self.pages_fetched,
            "truncated": self.truncated,
            **self.stats,
            "source_stats": self.source_stats,
        }


def _tag(exc: BaseException, recorder) -> None:
    """Stamp the run id on a failure, so a caller holding only the exception can
    still tell the tenant which run to look at."""
    try:
        exc.run_id = recorder.run_id
    except Exception:  # noqa: BLE001 - a read-only exception must not mask the real one
        pass


def run_api_sync(params: SyncParams, *, recorder, user=None) -> SyncResult:
    """Execute the sync, recording each stage on ``recorder``.

    Raises :class:`EmptySource`, :class:`IngestionBlocked`,
    ``requests.RequestException` or :class:`APIContentProcessingError`; each is
    written to the run record before it propagates. The analysis and embedding
    calls are charged to ``user`` (points).
    """
    from ..models import UsageKind
    from . import billing

    with billing.charge_to(user, UsageKind.API_SYNC):
        return _run_api_sync(params, recorder=recorder, user=user)


def _run_api_sync(params: SyncParams, *, recorder, user=None) -> SyncResult:
    try:
        # Fetch the WHOLE source: every page, and every field of each page --
        # not just the first page's items list.
        logger.info("Fetching from %s", params.api_url)
        payloads, truncated = fetch_all_pages(params.api_url, max_pages=params.max_pages)
        items, pages = extract_all(payloads, params.items_key)

        if not items:
            recorder.failed("The API response contained no content to import.")
            raise EmptySource("The API response contained no content to import.")

        logger.info("Fetched %d records across %d page(s)", len(items), len(payloads))

        # The census of the payload as it arrived. Everything the response later
        # claims about completeness is measured against this.
        census = source_statistics(
            items,
            endpoint=params.api_url,
            pages_fetched=len(payloads),
            truncated=truncated,
        )
        recorder.fetched(
            pages=len(payloads),
            records=len(items),
            truncated=truncated,
            source_stats=census,
        )

        processor = APIContentRAGProcessor(
            document_name=params.document_name,
            user=user,
            api_url=params.api_url,
            items_key=params.items_key,
        )
        recorder.stage("embedding", document=processor.api_document)
        stats = processor.process_items(
            items,
            full_refresh=params.full_refresh,
            ai_analysis=params.ai_analysis,
            pages=pages,
        )
        recorder.succeeded(stats)

        return SyncResult(
            pages_fetched=len(payloads),
            truncated=truncated,
            stats=stats,
            source_stats=census,
        )

    except EmptySource as exc:
        _tag(exc, recorder)
        raise
    except IngestionBlocked as exc:
        # Quality control refused this import. The tenant's previous knowledge
        # base is still live and unchanged.
        logger.error("Ingestion blocked: %s", exc.reason)
        recorder.blocked(exc.reason, exc.report)
        _tag(exc, recorder)
        raise
    except requests.RequestException as exc:
        logger.error("API request failed: %s", exc)
        recorder.failed(str(exc))
        _tag(exc, recorder)
        raise
    except APIContentProcessingError as exc:
        logger.error("Processing failed: %s", exc)
        recorder.failed(str(exc))
        _tag(exc, recorder)
        raise
    except Exception as exc:
        logger.exception("Unexpected error while syncing API content")
        recorder.failed("unexpected error")
        _tag(exc, recorder)
        raise


class QueueUnavailable(Exception):
    """The broker refused the job, so nothing will ever run it."""


def _run_in_thread(run_id, params_dict: dict, user_id) -> None:
    """Single-server background mode: same work, no broker."""
    from django.contrib.auth import get_user_model
    from django.db import connection

    from .api_ingest_run import IngestionRunRecorder

    try:
        user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
        run_api_sync(SyncParams(**params_dict), recorder=IngestionRunRecorder.resume(run_id), user=user)
    except Exception:
        # run_api_sync already recorded the outcome on the run row.
        logger.warning("Background API sync %s did not complete", run_id, exc_info=True)
    finally:
        # Each thread gets its own DB connection; close it so it isn't leaked.
        connection.close()


def dispatch_api_sync(params: SyncParams, *, user=None) -> tuple[str, SyncResult | None]:
    """Run the sync per ``INGESTION_MODE``; return ``(run_id, result_or_None)``.

    ``sync`` runs it in-request and returns the result. ``celery`` and ``thread``
    hand it off and return ``None``, so the caller answers immediately with the
    run id and the tenant polls that instead of holding a connection open for
    the length of an LLM fan-out -- which is what makes a large import a 504
    rather than an import.
    """
    import threading

    from django.conf import settings

    from ..models import IngestionStage
    from .api_ingest_run import IngestionRunRecorder

    mode = str(getattr(settings, "INGESTION_MODE", "sync")).lower()
    user_id = str(user.pk) if user is not None and getattr(user, "is_authenticated", False) else None

    if mode not in {"celery", "thread"}:
        recorder = IngestionRunRecorder(user=user, endpoint=params.api_url)
        return recorder.run_id, run_api_sync(params, recorder=recorder, user=user)

    # Open the run first: the tenant needs an id to poll before any work starts.
    recorder = IngestionRunRecorder(user=user, endpoint=params.api_url, stage=IngestionStage.QUEUED)
    run_id = recorder.run_id

    if mode == "thread":
        threading.Thread(
            target=_run_in_thread,
            args=(run_id, params.as_dict(), user_id),
            daemon=True,
        ).start()
        logger.info("Queued background API sync %s", run_id)
        return run_id, None

    from ..tasks import sync_api_content_task

    # Enqueued directly rather than via transaction.on_commit: ATOMIC_REQUESTS is
    # off, so the run row above is already committed and the worker can see it.
    # Deferring would also swallow a broker failure -- the hook runs after the
    # response is decided, leaving the tenant a 202 for a job nobody accepted.
    try:
        sync_api_content_task.delay(run_id, user_id, params.as_dict())
    except Exception as exc:
        # A broker that will not take the job leaves the run queued forever.
        # Say so now rather than letting the tenant poll a row nothing advances.
        logger.exception("Could not enqueue API sync %s", run_id)
        recorder.failed(f"Could not queue the import: {exc}")
        raise QueueUnavailable(str(exc)) from exc

    logger.info("Enqueued Celery API sync %s", run_id)
    return run_id, None
