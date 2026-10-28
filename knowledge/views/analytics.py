"""Per-tenant usage analytics.

``GET /api/v1/analytics/usage/``
  Returns the authenticated tenant's own rollup (totals, this-month, today) plus
  live quota consumption (documents/storage used vs. their limits).

``GET /api/v1/analytics/usage/?scope=all``  (admin only)
  Returns a per-tenant summary across all tenants.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication

from ..models import UsageKind, UsageRecord
from ..services import quota

User = get_user_model()


def _rollup(qs) -> dict:
    # Requests, latency and confidence are about chat answers; tokens, cost and
    # points cover every metered operation (documents, translation, ...).
    chat = Q(kind=UsageKind.CHAT)
    agg = qs.aggregate(
        requests=Count("id", filter=chat),
        tokens_in=Sum("tokens_in"),
        tokens_out=Sum("tokens_out"),
        cost=Sum("cost_usd"),
        points=Sum("points_milli"),
        avg_ms=Avg("response_time_ms", filter=chat),
        confident=Count("id", filter=chat & Q(confident=True)),
    )
    requests = agg["requests"] or 0
    confident = agg["confident"] or 0
    return {
        "requests": requests,
        "tokens_in": agg["tokens_in"] or 0,
        "tokens_out": agg["tokens_out"] or 0,
        "total_tokens": (agg["tokens_in"] or 0) + (agg["tokens_out"] or 0),
        "cost_usd": round(agg["cost"] or 0.0, 4),
        "points_used": round((agg["points"] or 0) / 1000, 3),
        "avg_response_ms": round(agg["avg_ms"] or 0.0, 1),
        "confident_rate": round(confident / requests, 3) if requests else 0.0,
    }


def _tenant_payload(user) -> dict:
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    base = UsageRecord.objects.filter(user=user)
    limits = quota.effective_limits(user)
    doc_count, used_mb = quota.document_usage(user)

    return {
        "tenant": {"id": str(user.id), "username": user.username},
        "totals": _rollup(base),
        "this_month": _rollup(base.filter(created_at__gte=month_start)),
        "today": _rollup(base.filter(created_at__gte=day_start)),
        "quota": {
            "documents_used": doc_count,
            "max_documents": limits.max_documents,
            "storage_mb_used": round(used_mb, 2),
            "max_total_mb": limits.max_total_mb,
            "tokens_this_month": quota.tokens_used_this_month(user),
            "monthly_token_cap": limits.monthly_token_cap,  # 0 = unlimited
            "max_requests_per_min": limits.max_requests_per_min,
            "is_suspended": limits.is_suspended,
        },
    }


class UsageAnalyticsView(APIView):
    """Usage + quota analytics for the authenticated tenant (or all, for admins)."""

    authentication_classes = [APIKeyAuthentication, JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: dict})
    def get(self, request):
        scope = request.query_params.get("scope", "self")

        if scope == "all":
            if not (request.user.is_staff or request.user.is_superuser):
                return Response(
                    {"detail": "Admin access required for scope=all."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            tenants = User.objects.filter(usage_records__isnull=False).distinct()
            return Response(
                {"tenants": [_tenant_payload(u) for u in tenants]},
                status=status.HTTP_200_OK,
            )

        return Response(_tenant_payload(request.user), status=status.HTTP_200_OK)
