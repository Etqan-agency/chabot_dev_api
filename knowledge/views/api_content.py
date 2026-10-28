import logging

import requests
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..models import APIIngestionRun, IngestionStage
from ..services import quota
from ..services.api_content_processor import APIContentProcessingError, IngestionBlocked
from ..services.api_sync import (
    EmptySource,
    QueueUnavailable,
    SyncParams,
    dispatch_api_sync,
)
from ..services.net import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)


class SyncAPIContentView(APIView):
    """Ingest content from external API into RAG system."""

    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "ingest"

    @extend_schema(
        request={
            "type": "object",
            "properties": {
                "api_url": {"type": "string", "description": "API endpoint URL"},
                "document_name": {
                    "type": "string",
                    "description": "Virtual document name (default: 'API Content')",
                },
                "items_key": {
                    "type": "string",
                    "description": (
                        "JSON key containing the items list (default: 'results'). "
                        "Auto-detected when that key is absent; every other "
                        "top-level field of the response is imported as well."
                    ),
                },
                "ai_analysis": {
                    "type": "boolean",
                    "description": (
                        "Describe each record with the AI before indexing "
                        "(default: true). The description is stored alongside "
                        "the raw record, never instead of it."
                    ),
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Cap on pages followed for a paginated source.",
                },
            },
            "required": ["api_url"],
        },
        responses={
            200: {
                "type": "object",
                "description": "Completed in-request (INGESTION_MODE=sync).",
                "properties": {
                    "status": {"type": "string"},
                    "run_id": {"type": "string"},
                    "processed": {"type": "integer"},
                    "chunks_created": {"type": "integer"},
                    "errors": {"type": "integer"},
                },
            },
            202: {
                "type": "object",
                "description": (
                    "Accepted and running on a worker (INGESTION_MODE=celery/thread). "
                    "Poll status_url until status is completed, failed or blocked."
                ),
                "properties": {
                    "status": {"type": "string"},
                    "run_id": {"type": "string"},
                    "status_url": {"type": "string"},
                },
            },
        },
    )
    def post(self, request):
        """Fetch content from API and process for RAG."""
        # Feature gate: importing knowledge from an external API is not on the
        # free tier — upgrade required.
        try:
            from subscriptions.services import can_sync_api_content

            allowed = can_sync_api_content(request.user)
        except Exception:
            allowed = True  # fail open: never block on a billing-layer hiccup
        if not allowed:
            return Response(
                {
                    "detail": "Importing content from an external API isn't available "
                    "on the free plan. Upgrade to enable it."
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        quota.require_points(request.user)  # 402 when out of points / lapsed

        params = SyncParams.from_request_data(request.data)

        if not params.api_url:
            return Response(
                {"detail": "api_url is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SSRF guard: reject internal/reserved destinations before connecting.
        try:
            validate_public_url(params.api_url)
        except UnsafeURLError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            run_id, result = dispatch_api_sync(params, user=request.user)
        except EmptySource as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except IngestionBlocked as e:
            # Quality control refused this import. The tenant's previous
            # knowledge base is still live and unchanged -- say so plainly
            # rather than reporting a generic failure they cannot act on.
            return Response(
                {
                    "detail": (
                        "The imported data did not pass the completeness check, so it was not "
                        "applied. Your existing knowledge base is unchanged."
                    ),
                    "reason": e.reason,
                    "validation": e.report,
                    "run_id": getattr(e, "run_id", ""),
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except QueueUnavailable:
            return Response(
                {"detail": "The import queue is unavailable. Please try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except requests.RequestException:
            return Response(
                {"detail": "Could not fetch content from the provided URL."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except APIContentProcessingError:
            return Response(
                {"detail": "Failed to process the fetched content."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception:
            logger.exception("Unexpected error while syncing API content")
            return Response(
                {"detail": "An unexpected error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if result is None:
            # Handed to a worker. A full import runs an LLM call per record,
            # which outlives any gateway timeout, so the tenant gets the run id
            # and polls it instead of holding the connection open.
            return Response(
                {
                    "status": "queued",
                    "run_id": run_id,
                    "status_url": reverse("api-ingestion-run", args=[run_id], request=request),
                    "detail": "The import is running in the background. Poll status_url for progress.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        return Response(
            {"status": "success", "run_id": run_id, **result.as_dict()},
            status=status.HTTP_200_OK,
        )


class APIIngestionRunView(APIView):
    """Progress and outcome of one API sync.

    The polling half of the 202 the sync endpoint returns. Scoped to the
    requesting tenant: a run id is a UUID, but ownership is still checked, so a
    guessed id cannot reveal another tenant's endpoint or record counts.
    """

    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    TERMINAL = {IngestionStage.COMPLETED, IngestionStage.FAILED, IngestionStage.BLOCKED}

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "status": {"type": "string"},
                    "done": {"type": "boolean"},
                    "endpoint": {"type": "string"},
                    "records_fetched": {"type": "integer"},
                    "records_processed": {"type": "integer"},
                    "chunks_created": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                },
            },
        },
    )
    def get(self, request, run_id):
        run = APIIngestionRun.objects.filter(pk=run_id, user=request.user).first()
        if run is None:
            return Response({"detail": "No such import run."}, status=status.HTTP_404_NOT_FOUND)

        body = {
            "run_id": str(run.pk),
            "status": run.stage,
            "done": run.stage in self.TERMINAL,
            "endpoint": run.endpoint,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_ms": run.duration_ms,
            "pages_fetched": run.pages_fetched,
            "truncated": run.truncated,
            "records_fetched": run.records_fetched,
            "records_processed": run.records_processed,
            "chunks_created": run.chunks_created,
            "field_coverage": run.field_coverage,
            "source_stats": run.source_stats,
        }
        if run.stage == IngestionStage.COMPLETED:
            body["result"] = run.result_stats
        elif run.stage == IngestionStage.BLOCKED:
            body["detail"] = (
                "The imported data did not pass the completeness check, so it was not "
                "applied. Your existing knowledge base is unchanged."
            )
            body["reason"] = run.blocked_reason
            body["validation"] = (run.result_stats or {}).get("validation", {})
        elif run.stage == IngestionStage.FAILED:
            body["detail"] = run.error_message or "The import failed."

        return Response(body, status=status.HTTP_200_OK)
