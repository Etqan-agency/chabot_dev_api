import logging

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..services import quota
from ..services.net import UnsafeURLError, validate_public_url
from ..services.web_crawler import _default_limits, dispatch_site_crawl

logger = logging.getLogger(__name__)


class CrawlWebsiteView(APIView):
    """Crawl a client's website and ingest every page as RAG knowledge.

    Discovers as many pages of the same site as possible (sitemap + internal
    links), extracts each page's main readable text, and syncs them into the
    tenant's knowledge base. Re-crawling only re-embeds changed pages.
    """

    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "ingest"

    @extend_schema(
        request={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Website start URL (e.g. https://client.com)"},
                "document_name": {
                    "type": "string",
                    "description": "Name for the knowledge document (default: the site host).",
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Max pages to crawl (clamped to the server cap).",
                },
                "full_refresh": {
                    "type": "boolean",
                    "description": "Rebuild from scratch instead of incremental sync.",
                },
            },
            "required": ["url"],
        },
        responses={
            202: {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "document_id": {"type": "string"},
                    "document_name": {"type": "string"},
                    "max_pages": {"type": "integer"},
                    "detail": {"type": "string"},
                },
            },
        },
    )
    def post(self, request):
        # Feature gate: crawling a website into knowledge is a paid-plan feature
        # (same gate as external-API sync).
        try:
            from subscriptions.features import has_feature

            allowed = has_feature(request.user, "website_crawl")
        except Exception:
            allowed = True  # fail open: never block on a billing-layer hiccup
        if not allowed:
            return Response(
                {
                    "detail": "Crawling a website into your knowledge base isn't "
                    "available on the free plan. Upgrade to enable it."
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        quota.require_points(request.user)  # 402 when out of points / lapsed

        url = (request.data.get("url") or "").strip()
        if not url:
            return Response({"detail": "url is required"}, status=status.HTTP_400_BAD_REQUEST)
        if "://" not in url:
            url = "https://" + url

        # SSRF guard: reject internal/reserved destinations before connecting.
        try:
            validate_public_url(url)
        except UnsafeURLError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        from urllib.parse import urlparse

        host = urlparse(url).netloc or url
        document_name = (request.data.get("document_name") or f"Website: {host}").strip()

        default_pages, cap, timeout = _default_limits()
        try:
            requested = int(request.data.get("max_pages", default_pages))
        except (TypeError, ValueError):
            requested = default_pages
        max_pages = max(1, min(requested, cap))

        full_refresh = str(request.data.get("full_refresh", "")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        try:
            doc = dispatch_site_crawl(
                user=request.user,
                start_url=url,
                document_name=document_name,
                max_pages=max_pages,
                timeout=timeout,
                full_refresh=full_refresh,
            )
        except Exception:
            logger.exception("Failed to start website crawl")
            return Response(
                {"detail": "Could not start the crawl. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "status": "processing",
                "document_id": str(doc.pk),
                "document_name": document_name,
                "max_pages": max_pages,
                "detail": "Crawl started. Poll GET /documents/{id}/ for progress "
                "(processing_status: processing -> completed/failed).",
            },
            status=status.HTTP_202_ACCEPTED,
        )
