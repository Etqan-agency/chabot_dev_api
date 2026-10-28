"""Paddle Billing webhook: verify signature, then fulfill (grant plan/points).

Not used while plans are assigned and renewed by an admin by hand; kept for a
future self-serve checkout.

Design:
* Money → points. Every successful payment is a ``transaction.completed`` event
  (initial AND renewals); that is the ONLY place we grant points, so a plan is
  never double-granted. Each payment resets the wallet to the plan's points.
* Plan state → ``subscription.*`` events set/refresh/cancel the Subscription row
  (plan, period, status, Paddle ids) but never grant points.

A tenant is matched from ``custom_data.user_id`` (the frontend passes it into the
Paddle checkout); a Plan is matched from the item's ``price.id`` →
``Plan.paddle_price_id``. Every event id is recorded so re-deliveries are no-ops.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.dateparse import parse_datetime

from .models import PaddleWebhookEvent, Plan
from .services import cancel_paddle_subscription, set_paddle_subscription, set_points

logger = logging.getLogger(__name__)
User = get_user_model()


def verify_signature(raw_body: bytes, signature_header: str, secret: str | None = None) -> bool:
    """Verify Paddle's ``Paddle-Signature: ts=...;h1=...`` header.

    HMAC-SHA256 over ``"{ts}:" + raw_body`` with the webhook secret must equal h1.
    """
    secret = secret if secret is not None else getattr(settings, "PADDLE_WEBHOOK_SECRET", "")
    if not secret or not signature_header:
        return False
    parts = {}
    for piece in signature_header.split(";"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1:
        return False
    signed = ts.encode() + b":" + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


def _user_from(data: dict):
    cd = data.get("custom_data") or {}
    uid = cd.get("user_id") if isinstance(cd, dict) else None
    if not uid:
        return None
    return User.objects.filter(pk=uid).first()


def _plan_from_items(data: dict):
    for item in data.get("items", []) or []:
        price = item.get("price") or {}
        pid = price.get("id") or item.get("price_id")
        if pid:
            plan = Plan.objects.filter(paddle_price_id=pid).first()
            if plan is not None:
                return plan
    return None


def process_event(payload: dict) -> str:
    """Fulfill one webhook payload. Returns a short status string (for logging).

    Idempotent: a previously-seen ``event_id`` is skipped.
    """
    event_id = payload.get("event_id") or payload.get("notification_id")
    event_type = payload.get("event_type") or ""
    data = payload.get("data") or {}

    if event_id:
        _, created = PaddleWebhookEvent.objects.get_or_create(event_id=event_id, defaults={"event_type": event_type})
        if not created:
            return "duplicate"

    if event_type == "transaction.completed":
        return _grant_for_transaction(data)
    if event_type in {"subscription.created", "subscription.activated", "subscription.updated"}:
        return _set_subscription(data)
    if event_type in {"subscription.canceled", "subscription.paused"}:
        return _cancel_subscription(data)
    return "ignored"


def _grant_for_transaction(data: dict) -> str:
    user = _user_from(data)
    plan = _plan_from_items(data)
    if user is None or plan is None:
        logger.warning("Paddle transaction unmatched (user=%s plan=%s)", bool(user), bool(plan))
        return "unmatched"
    if plan.points:
        set_points(user, plan.points)  # a paid period resets the points (no rollover)
    return "points_granted"


def _set_subscription(data: dict) -> str:
    user = _user_from(data)
    plan = _plan_from_items(data)
    if user is None or plan is None:
        logger.warning("Paddle subscription unmatched (user=%s plan=%s)", bool(user), bool(plan))
        return "unmatched"
    set_paddle_subscription(
        user,
        plan,
        paddle_subscription_id=data.get("id", "") or "",
        paddle_customer_id=data.get("customer_id", "") or "",
        period_end=parse_datetime(data.get("next_billed_at") or "") if data.get("next_billed_at") else None,
    )
    return "subscription_set"


def _cancel_subscription(data: dict) -> str:
    cancel_paddle_subscription(data.get("id", "") or "")
    return "subscription_canceled"
