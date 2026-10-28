"""Observability for one API ingestion attempt.

A sync touches a remote server, an LLM, an embedding provider and the index, and
any of them can be the reason a tenant's answers got worse. This records what
actually happened at each stage -- with real counts from the run itself, never
estimates -- so that question is answerable afterwards instead of by guessing.

Credentials never reach this table: the endpoint is stored with its query string
stripped, and error text is truncated and taken from exception messages, which
the fetch layer raises without echoing request headers.
"""

from __future__ import annotations

import logging
import re
import time

from django.utils import timezone

from ..models import APIIngestionRun, IngestionStage

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 2000


def scrub_endpoint(url: str) -> str:
    """Drop the query string: it is where API keys and tokens travel."""
    if not url:
        return ""
    return re.sub(r"[?#].*$", "", str(url))[:200]


class IngestionRunRecorder:
    """Tracks one sync. Never raises -- observability must not break ingestion."""

    def __init__(self, *, user=None, document=None, endpoint: str = "", stage: str = IngestionStage.FETCHING):
        self.started = time.monotonic()
        self.run: APIIngestionRun | None = None
        try:
            self.run = APIIngestionRun.objects.create(
                user=user if user and getattr(user, "is_authenticated", False) else None,
                document=document,
                endpoint=scrub_endpoint(endpoint),
                stage=stage,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not open an ingestion run record", exc_info=True)

    @classmethod
    def resume(cls, run_id) -> IngestionRunRecorder:
        """Re-attach to a run opened elsewhere -- the queued/worker handover.

        The view opens the run so it can hand the tenant an id to poll, then the
        worker picks that same row up instead of opening a second one. A row
        that has since vanished yields an inert recorder rather than an error:
        losing the bookkeeping must never cost the tenant the sync itself.
        """
        self = cls.__new__(cls)
        self.started = time.monotonic()
        self.run = None
        try:
            self.run = APIIngestionRun.objects.get(pk=run_id)
        except APIIngestionRun.DoesNotExist:
            logger.warning("Ingestion run %s no longer exists; proceeding unrecorded", run_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not re-open ingestion run %s", run_id, exc_info=True)
        return self

    @property
    def run_id(self) -> str:
        return str(self.run.pk) if self.run is not None else ""

    # -- stage transitions --------------------------------------------------
    def stage(self, stage: str, **fields) -> None:
        self._save(stage=stage, **fields)

    def fetched(self, *, pages: int, records: int, truncated: bool, source_stats: dict) -> None:
        self._save(
            stage=IngestionStage.STRUCTURING,
            pages_fetched=pages,
            records_fetched=records,
            truncated=truncated,
            source_stats=source_stats,
        )

    def succeeded(self, stats: dict) -> None:
        self._save(
            stage=IngestionStage.COMPLETED,
            records_processed=int(stats.get("processed") or 0),
            chunks_created=int(stats.get("chunks_created") or 0),
            embeddings_created=int(stats.get("chunks_created") or 0),
            fields_total=int(stats.get("fields_total") or 0),
            fields_covered=int(stats.get("fields_covered") or 0),
            result_stats=stats,
            finished=True,
        )

    def blocked(self, reason: str, report: dict) -> None:
        self._save(
            stage=IngestionStage.BLOCKED,
            blocked_reason=str(reason)[:MAX_ERROR_CHARS],
            fields_total=int(report.get("fields_total") or 0),
            fields_covered=int(report.get("fields_covered") or 0),
            result_stats={"validation": report},
            finished=True,
        )

    def failed(self, error: str) -> None:
        self._save(
            stage=IngestionStage.FAILED,
            error_message=str(error)[:MAX_ERROR_CHARS],
            finished=True,
        )

    # -- internals ----------------------------------------------------------
    def _save(self, *, finished: bool = False, **fields) -> None:
        if self.run is None:
            return
        try:
            for name, value in fields.items():
                setattr(self.run, name, value)
            if finished:
                self.run.finished_at = timezone.now()
                self.run.duration_ms = int((time.monotonic() - self.started) * 1000)
            self.run.save()
        except Exception:  # noqa: BLE001 - never let logging break a sync
            logger.warning("Could not update ingestion run record", exc_info=True)
