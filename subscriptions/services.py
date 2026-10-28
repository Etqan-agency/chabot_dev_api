"""Subscription resolution helpers used by the quota/enforcement layer.

Kept dependency-light: ``knowledge.services.quota`` imports these lazily so the
two apps don't hard-couple. A user with NO subscription falls back to the global
default limits (so existing tenants keep working as a free tier).
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import CreditWallet, Plan, Subscription, SubscriptionStatus


def resolve_plan(ref):
    """Look up a Plan by slug or UUID. Returns the Plan or None."""
    if not ref:
        return None
    plan = Plan.objects.filter(slug=str(ref)).first()
    if plan is not None:
        return plan
    try:
        return Plan.objects.filter(pk=ref).first()
    except Exception:
        return None


def assign_plan(user, plan, *, duration_days=30, auto_renew=True):
    """Put ``user`` on ``plan`` (Plan instance or slug/UUID) for a period.

    Reusable for admin assignment and a future self-serve signup. Returns the
    Subscription, or None if the plan couldn't be resolved.
    """
    if not isinstance(plan, Plan):
        plan = resolve_plan(plan)
    if plan is None:
        return None
    now = timezone.now()
    sub, _ = Subscription.objects.update_or_create(
        user=user,
        defaults={
            "plan": plan,
            "status": SubscriptionStatus.ACTIVE,
            "current_period_start": now,
            "current_period_end": now + timedelta(days=int(duration_days or 30)),
            "auto_renew": auto_renew,
        },
    )
    # Reset the wallet to the plan's points: no rollover, and moving a user
    # between plans RESETS the balance to the new plan's points (a downgrade
    # shrinks it, an upgrade grows it) rather than stacking. A plan that grants
    # no points (points == 0) leaves the balance alone, so a limits-only plan
    # change never silently wipes a wallet.
    if plan.points:
        set_points(user, plan.points)
    _resume_paused_work(user)
    return sub


# --- Manual renewal ---------------------------------------------------------


def renewal_reason(sub: Subscription) -> str | None:
    """Why ``sub`` may be renewed now: "expired" (its period ended or it was
    canceled) or "points_exhausted" (less than one point left), else None --
    a running subscription with points left is not renewable."""
    if not sub.is_current:
        return "expired"
    if not has_points(sub.user):
        return "points_exhausted"
    return None


def renew_subscription(sub: Subscription, *, duration_days: int | None = None) -> Subscription:
    """Start a new period on the SAME plan: the period restarts now (for the
    previous period's length unless ``duration_days`` is given) and the wallet
    is reset to the plan's points. The caller checks ``renewal_reason`` first."""
    if duration_days is None:
        previous = round((sub.current_period_end - sub.current_period_start).total_seconds() / 86400)
        duration_days = previous if previous > 0 else 30
    return assign_plan(sub.user, sub.plan, duration_days=duration_days, auto_renew=sub.auto_renew)


def _resume_paused_work(user) -> None:
    """New points: finish what stopped when the old ones ran out (the tree's
    untranslated nodes are retried by a sync)."""
    try:
        from knowledge.models import QuestionTreeNode
        from knowledge.services.guided_tree import active_target_languages, schedule_sync

        if QuestionTreeNode.objects.filter(owner=user).exists() and active_target_languages(user):
            schedule_sync(user)
    except Exception:
        import logging

        logging.getLogger(__name__).warning("Could not resume tree translation for %s", user.pk, exc_info=True)


# --- Points -----------------------------------------------------------------

MILLI_PER_POINT = 1000


def _free_milli() -> int:
    return int(getattr(settings, "FREE_TIER_POINTS", 0)) * MILLI_PER_POINT


def get_wallet(user) -> CreditWallet:
    """The tenant's wallet, created on first use with the free-tier points.

    Free-tier wallets RENEW monthly (lazily): on the first access in a new
    calendar month the balance is reset to ``FREE_TIER_POINTS`` (no rollover).
    A tenant with a subscription is skipped; its points come from the plan.
    """
    wallet, created = CreditWallet.objects.get_or_create(
        user=user, defaults={"milli_points": _free_milli(), "granted_milli": _free_milli()}
    )
    if not created:
        _maybe_renew_free_points(wallet, user)
    return wallet


def _maybe_renew_free_points(wallet: CreditWallet, user) -> None:
    """Reset a free-tier wallet to FREE_TIER_POINTS once per calendar month.
    The conditional UPDATE is race-safe: concurrent first-of-month requests
    apply the grant exactly once. A lapsed subscription is not the free tier
    (it waits for the admin to renew), so it gets nothing here."""
    if is_points_exempt(user) or get_subscription(user) is not None:
        return
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if wallet.last_free_grant_at >= month_start:
        return  # already granted this month
    updated = CreditWallet.objects.filter(pk=wallet.pk, last_free_grant_at__lt=month_start).update(
        milli_points=_free_milli(),
        granted_milli=_free_milli(),
        last_free_grant_at=now,
    )
    if updated:
        wallet.refresh_from_db(fields=["milli_points", "granted_milli", "last_free_grant_at"])


def points_milli(user) -> int:
    """The wallet balance in milli-points."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return get_wallet(user).milli_points


def points_balance(user) -> float:
    """The wallet balance in points (3 decimals)."""
    return round(points_milli(user) / MILLI_PER_POINT, 3)


def points_allowance(user) -> int:
    """The points granted this period: the plan's (else the free tier's) plus
    any admin top-up -- the total that "used" is counted against."""
    sub = get_subscription(user)
    base = sub.plan.points if sub is not None else int(getattr(settings, "FREE_TIER_POINTS", 0))
    if user is None or not getattr(user, "is_authenticated", False):
        return base
    granted = get_wallet(user).granted_milli // MILLI_PER_POINT
    return granted if granted > 0 else base


def is_points_exempt(user) -> bool:
    """Staff/superusers never spend points — their usage is internal testing.

    It is still recorded in UsageRecord (tokens/cost analytics), just never
    charged and never blocked with 402.
    """
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def has_points(user) -> bool:
    """At least one whole point left (or exempt)."""
    if is_points_exempt(user):
        return True
    return points_milli(user) >= MILLI_PER_POINT


def set_points(user, points: int) -> int:
    """Reset the wallet to exactly ``points`` (a new period's grant). Returns
    the new balance in milli-points."""
    milli = max(0, int(points or 0)) * MILLI_PER_POINT
    wallet = get_wallet(user)
    CreditWallet.objects.filter(pk=wallet.pk).update(milli_points=milli, granted_milli=milli)
    return milli


def add_points(user, points: int) -> int:
    """Add ``points`` (an admin top-up) to the balance and to this period's
    total. Returns the new balance in milli-points."""
    milli = max(0, int(points or 0)) * MILLI_PER_POINT
    wallet = get_wallet(user)
    if milli:
        CreditWallet.objects.filter(pk=wallet.pk).update(
            milli_points=F("milli_points") + milli, granted_milli=F("granted_milli") + milli
        )
        wallet.refresh_from_db(fields=["milli_points"])
    return wallet.milli_points


def charge_points(user, milli: int) -> None:
    """Take ``milli`` milli-points for work already done, never below zero
    (the call has been paid to OpenAI either way; the next one is refused)."""
    if is_points_exempt(user) or milli <= 0:
        return
    wallet = get_wallet(user)
    CreditWallet.objects.filter(pk=wallet.pk).update(milli_points=Greatest(F("milli_points") - milli, Value(0)))


def reserve_points(user, milli: int) -> int | None:
    """ATOMICALLY hold up to ``milli`` milli-points before a chat answer.

    Returns what was held -- the full amount, or everything left when that is
    less (but at least one point) -- or None when the wallet is below one
    point. Each step is one conditional UPDATE, so concurrent requests can
    never hold more than the wallet has.
    """
    if is_points_exempt(user):
        return 0
    wallet = get_wallet(user)
    rows = CreditWallet.objects.filter(pk=wallet.pk)
    if milli > 0 and rows.filter(milli_points__gte=milli).update(milli_points=F("milli_points") - milli):
        return milli
    for _ in range(5):  # less than the full hold left: take all of it (compare-and-set)
        balance = rows.values_list("milli_points", flat=True).first() or 0
        if balance < MILLI_PER_POINT:
            return None
        if rows.filter(milli_points=balance).update(milli_points=0):
            return balance
    return None


def settle_points(user, reserved: int, actual: int) -> None:
    """Settle a hold to the real cost: give back ``reserved - actual``, or take
    the excess when the answer cost more than was held (never below zero)."""
    if is_points_exempt(user):
        return
    wallet = get_wallet(user)
    rows = CreditWallet.objects.filter(pk=wallet.pk)
    delta = int(reserved or 0) - int(actual or 0)
    if delta > 0:
        rows.update(milli_points=F("milli_points") + delta)
    elif delta < 0:
        rows.update(milli_points=Greatest(F("milli_points") + delta, Value(0)))


def set_paddle_subscription(user, plan, *, paddle_subscription_id="", paddle_customer_id="", period_end=None):
    """Create/refresh a Subscription from a Paddle event WITHOUT granting points.

    Points are granted separately by ``transaction.completed`` so each payment
    grants exactly once (see subscriptions/paddle.py).
    """
    now = timezone.now()
    sub, _ = Subscription.objects.update_or_create(
        user=user,
        defaults={
            "plan": plan,
            "status": SubscriptionStatus.ACTIVE,
            "current_period_start": now,
            "current_period_end": period_end or (now + timedelta(days=30)),
            "auto_renew": True,
            "paddle_subscription_id": paddle_subscription_id,
            "paddle_customer_id": paddle_customer_id,
        },
    )
    return sub


def cancel_paddle_subscription(paddle_subscription_id: str):
    """Mark the subscription linked to this Paddle id as canceled."""
    if not paddle_subscription_id:
        return None
    sub = Subscription.objects.filter(paddle_subscription_id=paddle_subscription_id).first()
    if sub is not None:
        sub.status = SubscriptionStatus.CANCELED
        sub.save(update_fields=["status", "updated_at"])
    return sub


def get_subscription(user):
    """Return the user's Subscription row, or None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        return user.subscription
    except Subscription.DoesNotExist:
        return None


def active_plan(user):
    """Return the user's plan IF their subscription is active & in-period, else None."""
    sub = get_subscription(user)
    if sub and sub.is_current:
        return sub.plan
    return None


def subscription_state(user) -> str:
    """One of: 'none' (no subscription), 'active', or 'inactive' (expired/canceled)."""
    sub = get_subscription(user)
    if sub is None:
        return "none"
    return "active" if sub.is_current else "inactive"


def period_start(user):
    """Start of the current usage window: the subscription period, else None
    (callers fall back to a calendar month)."""
    sub = get_subscription(user)
    if sub and sub.is_current:
        return sub.current_period_start
    return None


def resolve_model(user):
    """LLM model for this user's plan, or None to use the global default."""
    plan = active_plan(user)
    return plan.llm_model if plan and plan.llm_model else None


def can_sync_api_content(user) -> bool:
    """May this tenant import knowledge from an external API (sync-api-content)?

    Delegates to the feature system so a per-user admin override applies. Default
    (no override): staff always; paid plan per ``allow_api_sync``; free tier per
    ``settings.FREE_TIER_ALLOW_API_SYNC``.
    """
    from .features import has_feature

    return has_feature(user, "api_sync")


def plan_limits(user):
    """Return the active plan's limits as a dict, or None if no active plan.

    Keys mirror EffectiveLimits fields so the quota layer can merge them.
    """
    plan = active_plan(user)
    if plan is None:
        return None
    return {
        "max_documents": plan.max_documents,
        "max_total_mb": plan.max_total_mb,
        "max_requests_per_min": plan.max_requests_per_min,
        "monthly_token_cap": plan.monthly_token_cap,
        "monthly_questions": plan.monthly_questions,
    }
