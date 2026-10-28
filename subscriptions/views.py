from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from Authentication.authentication import APIKeyAuthentication
from knowledge.services import quota

from .models import Plan, Subscription
from .serializers import (
    AssignSubscriptionSerializer,
    PlanSerializer,
    PointsHistorySerializer,
    RenewSubscriptionSerializer,
    SubscriptionSerializer,
)

AUTH = [APIKeyAuthentication, JWTAuthentication, SessionAuthentication]


class IsAdminOrReadOnly(permissions.BasePermission):
    """Anyone authenticated can read plans; only admins can change them."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_staff


class PlanViewSet(viewsets.ModelViewSet):
    """Browse plans (any authenticated user); manage them (admin only)."""

    queryset = Plan.objects.all()
    serializer_class = PlanSerializer
    authentication_classes = AUTH
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        qs = Plan.objects.all()
        # Non-admins only see active, sellable plans.
        if not (self.request.user and self.request.user.is_staff):
            qs = qs.filter(is_active=True)
        return qs


class SubscriptionViewSet(viewsets.ModelViewSet):
    """Admin-only control plane for assigning/inspecting subscriptions."""

    queryset = Subscription.objects.select_related("user", "plan", "user__credit_wallet").all()
    serializer_class = SubscriptionSerializer
    authentication_classes = AUTH
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=AssignSubscriptionSerializer, responses={200: SubscriptionSerializer})
    def create(self, request, *args, **kwargs):
        """Assign (or move) a user to a plan for a billing period."""
        serializer = AssignSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sub = serializer.save()
        return Response(SubscriptionSerializer(sub).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=RenewSubscriptionSerializer,
        responses={200: SubscriptionSerializer},
        description=(
            "Admin: renew a subscription on the same plan -- a new period starting now and the "
            "points reset to the plan's. Only when its period ended or its points ran out (409 otherwise)."
        ),
    )
    @action(detail=True, methods=["post"], url_path="renew")
    def renew(self, request, pk=None):
        from .services import renew_subscription, renewal_reason

        sub = self.get_object()
        serializer = RenewSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if renewal_reason(sub) is None:
            return Response(
                {
                    "detail": "This subscription is still running and has points left. "
                    "It can be renewed once its period ends or its points run out.",
                    "code": "not_renewable",
                },
                status=status.HTTP_409_CONFLICT,
            )
        sub = renew_subscription(sub, duration_days=serializer.validated_data.get("duration_days"))
        return Response(SubscriptionSerializer(sub).data)

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "User id"},
                    "amount": {"type": "integer", "description": "Points to add"},
                },
                "required": ["user", "amount"],
            }
        },
        responses={200: dict},
        description="Admin: add points to a user's wallet (a manual correction).",
    )
    @action(detail=False, methods=["post"], url_path="add-points")
    def add_points(self, request):
        from django.contrib.auth import get_user_model

        from .services import add_points, points_balance

        user_id = request.data.get("user")
        try:
            amount = int(request.data.get("amount"))
        except (TypeError, ValueError):
            return Response({"detail": "amount must be an integer."}, status=400)
        if amount <= 0:
            return Response({"detail": "amount must be positive."}, status=400)
        target = get_user_model().objects.filter(pk=user_id).first()
        if target is None:
            return Response({"detail": "Unknown user."}, status=404)
        add_points(target, amount)
        return Response({"user": str(target.pk), "points_balance": points_balance(target)})


class PaddleWebhookView(APIView):
    """Public endpoint Paddle calls after a payment. Verifies the signature, then
    grants credits / sets the subscription. Never trusts an unsigned request."""

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    @extend_schema(request=None, responses={200: None, 403: None})
    def post(self, request):
        import json
        import logging

        from .paddle import process_event, verify_signature

        raw = request.body
        signature = request.headers.get("paddle-signature", "")
        if not verify_signature(raw, signature):
            return Response({"detail": "Invalid signature."}, status=status.HTTP_403_FORBIDDEN)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = process_event(payload)
        except Exception:
            logging.getLogger(__name__).exception("Paddle webhook processing failed")
            # 200 so Paddle doesn't hammer retries on a bug we've already logged.
            return Response({"status": "error-logged"})
        return Response({"status": result})


class FeatureCatalogView(APIView):
    """The list of controllable features (keys + labels) for the dashboard UI."""

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: dict})
    def get(self, request):
        from .features import catalog

        return Response({"features": catalog()})


class MyFeaturesView(APIView):
    """The caller's own resolved features (so the frontend shows/hides UI)."""

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: dict})
    def get(self, request):
        from .features import resolved_features

        return Response({"features": resolved_features(request.user)})


class UserFeaturesView(APIView):
    """Admin: read or set which features a specific user has.

    ``PATCH`` body: ``{"overrides": {"api_sync": true, "website_crawl": false,
    "api_key": null}}`` — ``true``/``false`` force it, ``null`` clears the
    override (inherit the plan/tier default). Unknown keys are ignored.
    """

    authentication_classes = AUTH
    permission_classes = [permissions.IsAdminUser]

    def _user(self, user_id):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.filter(pk=user_id).first()

    @extend_schema(responses={200: dict})
    def get(self, request, user_id):
        user = self._user(user_id)
        if user is None:
            return Response({"detail": "Unknown user."}, status=status.HTTP_404_NOT_FOUND)
        from .features import _overrides, resolved_features

        return Response(
            {
                "user": str(user.pk),
                "username": user.username,
                "features": resolved_features(user),
                "overrides": _overrides(user),
            }
        )

    @extend_schema(request={"type": "object"}, responses={200: dict})
    def patch(self, request, user_id):
        user = self._user(user_id)
        if user is None:
            return Response({"detail": "Unknown user."}, status=status.HTTP_404_NOT_FOUND)
        from .features import resolved_features, set_overrides

        mapping = request.data.get("overrides")
        if mapping is None:
            mapping = request.data
        if not isinstance(mapping, dict):
            return Response({"detail": "overrides must be an object."}, status=status.HTTP_400_BAD_REQUEST)
        stored = set_overrides(user, mapping)
        return Response(
            {
                "user": str(user.pk),
                "overrides": stored,
                "features": resolved_features(user),
            }
        )


class MySubscriptionView(APIView):
    """The caller's own subscription + live usage for the current period."""

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    @staticmethod
    def _remaining(used, limit):
        """Remaining allowance. None = unlimited (limit of 0)."""
        if not limit:
            return None
        return max(0, limit - used)

    @extend_schema(responses={200: dict})
    def get(self, request):
        user = request.user
        limits = quota.effective_limits(user)
        questions_used = quota.questions_used_this_period(user)
        tokens_used = quota.tokens_used_this_month(user)
        doc_count, used_mb = quota.document_usage(user)
        used_mb = round(used_mb, 2)

        # Documents/storage always have a positive cap (never "unlimited").
        docs_remaining = max(0, limits.max_documents - doc_count)
        storage_remaining = round(max(0.0, limits.max_total_mb - used_mb), 2)

        sub = None
        try:
            sub = user.subscription
        except Subscription.DoesNotExist:
            sub = None

        from .services import is_points_exempt, points_allowance, points_balance, renewal_reason

        balance = points_balance(user)
        allowance = points_allowance(user)
        exempt = is_points_exempt(user)

        return Response(
            {
                "subscription": SubscriptionSerializer(sub).data if sub else None,
                "on_free_tier": sub is None,
                "points": {
                    "balance": balance,
                    # Granted this period: the plan's (or free tier's) points plus admin top-ups.
                    "allowance": allowance,
                    "used": round(max(0.0, allowance - balance), 3),
                    # Admin/staff never spend points (internal testing).
                    "exempt": exempt,
                    "exhausted": not exempt and balance < 1,
                    "usd_per_point": 0.001,
                },
                # Set when the account is stopped and waits for the admin:
                # "expired" or "points_exhausted" (null on the free tier).
                "renewal_reason": renewal_reason(sub) if sub else None,
                "usage": {
                    "questions_used": questions_used,
                    "questions_limit": limits.monthly_questions,  # 0 = unlimited
                    "questions_remaining": self._remaining(questions_used, limits.monthly_questions),
                    "tokens_used": tokens_used,
                    "tokens_limit": limits.monthly_token_cap,  # 0 = unlimited
                    "tokens_remaining": self._remaining(tokens_used, limits.monthly_token_cap),
                    "documents_used": doc_count,
                    "documents_limit": limits.max_documents,
                    "documents_remaining": docs_remaining,
                    "storage_mb_used": used_mb,
                    "storage_mb_limit": limits.max_total_mb,
                    "storage_mb_remaining": storage_remaining,
                    "requests_per_min": limits.max_requests_per_min,
                },
            }
        )


class PointsHistoryView(APIView):
    """Where the points went: one row per metered operation, newest first.

    A tenant sees its own; an admin passes ``?user=<id>`` to see anyone's.
    Filter with ``?kind=chat`` (see ``UsageKind``).
    """

    authentication_classes = AUTH
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: PointsHistorySerializer(many=True)})
    def get(self, request):
        from rest_framework.pagination import PageNumberPagination

        from knowledge.models import UsageRecord

        target = request.user
        user_id = request.query_params.get("user")
        if user_id and str(user_id) != str(request.user.pk):
            if not request.user.is_staff:
                return Response({"detail": "Only admins can view another user's points."}, status=403)
            from django.contrib.auth import get_user_model

            target = get_user_model().objects.filter(pk=user_id).first()
            if target is None:
                return Response({"detail": "Unknown user."}, status=404)
        qs = UsageRecord.objects.filter(user=target).order_by("-created_at")
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(PointsHistorySerializer(page, many=True).data)
