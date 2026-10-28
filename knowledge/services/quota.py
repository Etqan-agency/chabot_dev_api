"""Per-tenant quota enforcement and usage metering.

A *tenant* is a ``User``. Limits resolve from the tenant's ``TenantQuota`` row,
falling back to the global ``TENANT_*`` defaults in settings. This module is the
single place that:

* answers "is this tenant allowed to do X right now?" (documents, size, rate,
  monthly tokens, suspension, points), and
* estimates the provider cost of a call (what points are charged from).

Views translate the raised exceptions into HTTP responses (413 / 429 / 403 /
402); ``require_points`` raises a DRF exception that renders by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import APIException

from ..models import TenantQuota, UploadedDocument, UsageKind, UsageRecord


class QuotaError(Exception):
    """Base class for quota violations (carries an HTTP status hint)."""

    status_code = 403

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TenantSuspended(QuotaError):
    status_code = 403


class DocumentQuotaExceeded(QuotaError):
    status_code = 413  # Payload/quota too large


class RateLimitExceeded(QuotaError):
    status_code = 429


class TokenBudgetExceeded(QuotaError):
    status_code = 429


class QuestionQuotaExceeded(QuotaError):
    status_code = 429


class SubscriptionInactive(QuotaError):
    status_code = 402  # Payment Required


class PointsExhausted(QuotaError):
    status_code = 402  # Payment Required — out of points until the plan is renewed


class QuotaDenied(APIException):
    """A ``QuotaError`` raised from a view: DRF renders it as ``{"detail": ...}``
    with the error's own status (402 / 403)."""

    def __init__(self, error: QuotaError):
        super().__init__(detail=error.message)
        self.status_code = error.status_code


# --- Limit resolution -------------------------------------------------------


@dataclass
class EffectiveLimits:
    max_documents: int
    max_total_mb: float
    max_requests_per_min: int
    monthly_token_cap: int  # 0 = unlimited
    is_suspended: bool
    monthly_questions: int = 0  # 0 = unlimited (from the active plan)


def get_quota(user) -> TenantQuota:
    quota, _ = TenantQuota.objects.get_or_create(user=user)
    return quota


def _plan_limits(user) -> dict | None:
    """Active subscription plan's limits, or None. Lazy import to avoid coupling."""
    try:
        from subscriptions.services import plan_limits
    except Exception:
        return None
    try:
        return plan_limits(user)
    except Exception:
        return None


def is_free_tier(user) -> bool:
    """A non-staff tenant with no active paid plan (a public free signup).

    Staff/admin and paid-plan tenants are NOT free tier, so the restrictive
    free-tier document/storage limits never apply to them.
    """
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return False
    return not _plan_limits(user)


def effective_limits(user) -> EffectiveLimits:
    """Resolve limits with precedence: per-tenant override > active plan > defaults.

    Free-tier tenants (no plan, not staff) get tiny document/storage defaults so
    they can only upload a small Word file (~3 pages).
    """
    quota = get_quota(user)
    plan = _plan_limits(user) or {}
    free = is_free_tier(user)

    def pick(quota_val, key, setting_name, default, cast):
        if quota_val is not None:
            return cast(quota_val)
        if key in plan and plan[key] is not None:
            return cast(plan[key])
        return cast(getattr(settings, setting_name, default))

    doc_setting = "FREE_TIER_MAX_DOCUMENTS" if free else "TENANT_MAX_DOCUMENTS"
    mb_setting = "FREE_TIER_MAX_TOTAL_MB" if free else "TENANT_MAX_TOTAL_MB"

    return EffectiveLimits(
        max_documents=pick(quota.max_documents, "max_documents", doc_setting, 1 if free else 100, int),
        max_total_mb=pick(quota.max_total_mb, "max_total_mb", mb_setting, 0.5 if free else 200, float),
        max_requests_per_min=pick(
            quota.max_requests_per_min, "max_requests_per_min", "TENANT_MAX_REQUESTS_PER_MIN", 60, int
        ),
        monthly_token_cap=pick(quota.monthly_token_cap, "monthly_token_cap", "TENANT_MONTHLY_TOKEN_CAP", 0, int),
        is_suspended=quota.is_suspended,
        monthly_questions=int(plan.get("monthly_questions") or 0),
    )


# --- Current consumption ----------------------------------------------------


def _docs_qs(user):
    return UploadedDocument.objects.filter(uploaded_by=user)


def document_usage(user) -> tuple[int, float]:
    """Return ``(document_count, total_mb)`` currently stored for the tenant."""
    agg = _docs_qs(user).aggregate(total=Sum("file_size"))
    total_bytes = agg["total"] or 0
    return _docs_qs(user).count(), total_bytes / (1024 * 1024)


def _month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def period_start(user):
    """Start of the current usage window — the subscription period if any, else
    the calendar month (so non-subscribers still get a monthly reset)."""
    try:
        from subscriptions.services import period_start as sub_period_start

        start = sub_period_start(user)
        if start is not None:
            return start
    except Exception:
        pass
    return _month_start()


def tokens_used_this_month(user) -> int:
    start = period_start(user)
    agg = UsageRecord.objects.filter(user=user, created_at__gte=start).aggregate(
        ti=Sum("tokens_in"), to=Sum("tokens_out")
    )
    return (agg["ti"] or 0) + (agg["to"] or 0)


def questions_used_this_period(user) -> int:
    start = period_start(user)
    return UsageRecord.objects.filter(user=user, kind=UsageKind.CHAT, created_at__gte=start).count()


def requests_in_last_minute(user) -> int:
    cutoff = timezone.now() - timedelta(seconds=60)
    return UsageRecord.objects.filter(user=user, kind=UsageKind.CHAT, created_at__gte=cutoff).count()


# --- Enforcement ------------------------------------------------------------


def check_not_suspended(user) -> None:
    if effective_limits(user).is_suspended:
        raise TenantSuspended("This account is suspended. Contact the administrator.")


def check_document_quota(user, incoming_bytes: int) -> None:
    """Raise if adding ``incoming_bytes`` would breach the tenant's doc limits,
    or the tenant has no points left to index it with."""
    check_points(user)
    limits = effective_limits(user)
    count, used_mb = document_usage(user)

    if count + 1 > limits.max_documents:
        raise DocumentQuotaExceeded(
            f"Document limit reached ({limits.max_documents}). Delete some documents or request a higher limit."
        )

    incoming_mb = (incoming_bytes or 0) / (1024 * 1024)
    if used_mb + incoming_mb > limits.max_total_mb:
        raise DocumentQuotaExceeded(
            f"Storage limit reached ({limits.max_total_mb:g} MB). Currently using {used_mb:.1f} MB."
        )


def _check_subscription_active(user) -> None:
    """If the user HAS a subscription that lapsed, block them. No subscription at
    all = allowed (free tier on default limits). Staff/superusers are exempt —
    their usage is internal testing and is never blocked."""
    try:
        from subscriptions.services import is_points_exempt, subscription_state
    except Exception:
        return
    if is_points_exempt(user):
        return
    if subscription_state(user) == "inactive":
        raise SubscriptionInactive("Your subscription has expired. Please renew to continue.")


POINTS_EXHAUSTED_MESSAGE = "You're out of points. Contact the administrator to renew your plan."


def check_points(user) -> None:
    """Raise unless ``user`` may spend points on OpenAI work right now: not
    suspended, subscription not lapsed, and at least one point left."""
    check_not_suspended(user)
    _check_subscription_active(user)
    from subscriptions.services import has_points

    if not has_points(user):
        raise PointsExhausted(POINTS_EXHAUSTED_MESSAGE)


def require_points(user) -> None:
    """``check_points`` for a view: an error becomes the HTTP response."""
    try:
        check_points(user)
    except QuotaError as exc:
        raise QuotaDenied(exc) from exc


def check_chat_allowed(user) -> None:
    """Raise if suspended, lapsed, rate-limited, over the question/token quota,
    or out of points."""
    check_not_suspended(user)
    _check_subscription_active(user)
    limits = effective_limits(user)

    if requests_in_last_minute(user) >= limits.max_requests_per_min:
        raise RateLimitExceeded(f"Rate limit exceeded ({limits.max_requests_per_min} requests/min). Please slow down.")

    if limits.monthly_questions and questions_used_this_period(user) >= limits.monthly_questions:
        raise QuestionQuotaExceeded(
            f"You've used all {limits.monthly_questions} questions in your plan for "
            f"this period. Upgrade your plan or wait for the next cycle."
        )

    if limits.monthly_token_cap and tokens_used_this_month(user) >= limits.monthly_token_cap:
        raise TokenBudgetExceeded("Usage budget exhausted for this period. It resets next cycle.")

    from subscriptions.services import has_points

    if not has_points(user):
        raise PointsExhausted(POINTS_EXHAUSTED_MESSAGE)


def reserve_chat_points(user) -> int | None:
    """ATOMICALLY hold the points of one answer BEFORE answering (race-safe).

    Holds ``CHAT_POINTS_RESERVE`` points (or what is left, when less). Returns
    the milli-points held -- settle them with ``billing.charge_to(...,
    reserved=...)`` -- or None when the wallet is empty (caller returns 402).
    Holding up front is what makes concurrent requests unable to overspend.
    """
    from subscriptions.services import reserve_points

    from .billing import MILLI_PER_POINT

    reserve = int(getattr(settings, "CHAT_POINTS_RESERVE", 20)) * MILLI_PER_POINT
    return reserve_points(user, reserve)


# --- Metering ---------------------------------------------------------------


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """USD cost of one call: an LLM (input/output prices) or an embedding model
    (one input price). An unknown model costs 0."""
    pricing = getattr(settings, "LLM_PRICING", {}).get(model)
    if not pricing:
        per_million = getattr(settings, "EMBEDDING_PRICING", {}).get(model)
        return (tokens_in / 1_000_000) * per_million if per_million else 0.0
    return (tokens_in / 1_000_000) * pricing.get("input", 0.0) + (tokens_out / 1_000_000) * pricing.get("output", 0.0)


def record_usage(
    user,
    *,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    response_time_ms: int = 0,
    confident: bool = True,
    chunk_count: int = 0,
    kind: str = UsageKind.CHAT,
) -> UsageRecord:
    """Persist one metered event and return it. Never raises for the caller."""
    return UsageRecord.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        kind=kind,
        model=model or "",
        tokens_in=max(0, tokens_in or 0),
        tokens_out=max(0, tokens_out or 0),
        cost_usd=estimate_cost(model, tokens_in or 0, tokens_out or 0),
        response_time_ms=max(0, response_time_ms or 0),
        confident=confident,
        chunk_count=max(0, chunk_count or 0),
    )
