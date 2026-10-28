"""Project-level views owned by no app: the liveness / drain probe."""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def _silence_request_error_log(response: JsonResponse) -> JsonResponse:
    """Keep a probe's 503 off the ``django.request`` logger.

    Django's ``log_response()`` logs every 5xx it returns to ``django.request``
    at ERROR. Traefik probes this endpoint every few seconds per replica, so a
    rolling deploy's drain window -- a deliberate 503, not a fault -- would
    emit one ERROR per probe for the length of the rollout and bury real
    failures in the noise.

    ``_has_been_logged`` is the same flag Django sets after logging a response,
    so pre-setting it makes ``log_response()`` skip this one. The health state
    is logged here instead, on this module's own logger.
    """
    response._has_been_logged = True
    return response


def health(_request):
    """Liveness and drain probe for Traefik, Docker and the deploy smoke checks.

    Returns 503 while the drain file exists so the replica leaves rotation
    before a rolling deploy stops it. See scripts/lib/rollout.sh.
    """
    try:
        connection.ensure_connection()
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    # Draining takes precedence: the container is being removed from rotation
    # deliberately and is not broken. The database state is still reported so
    # an operator can tell the two situations apart.
    if Path(settings.DRAIN_FILE).exists():
        logger.info("Health probe: draining (database=%s)", db_status)
        return _silence_request_error_log(JsonResponse({"status": "draining", "database": db_status}, status=503))

    if db_status == "connected":
        return JsonResponse({"status": "ok", "database": db_status})

    logger.error("Health probe: unhealthy (database=%s)", db_status)
    return _silence_request_error_log(JsonResponse({"status": "unhealthy", "database": db_status}, status=503))
