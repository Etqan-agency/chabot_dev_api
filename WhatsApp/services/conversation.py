"""Domain logic for WhatsApp conversations.

Handles user/session lookup, message logging, and the routing of incoming
messages to either a built-in command handler or the RAG service.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from knowledge.services.rag import RagUnavailable, answer_question
from knowledge.views.chat import detect_language
from voice.services import chat_ordering

from ..models import (
    WhatsAppAccount,
    WhatsAppMessage,
    WhatsAppSession,
    WhatsAppUser,
)

logger = logging.getLogger(__name__)


def resolve_account_and_tenant(phone_number_id: str):
    """Map the business number that received a message to (account, tenant).

    Falls back to the ``WHATSAPP_TENANT`` env username (single-tenant setups).
    Returns ``(account_or_None, tenant_or_None)``.
    """
    account = None
    if phone_number_id:
        account = (
            WhatsAppAccount.objects.filter(phone_number_id=phone_number_id, is_active=True)
            .select_related("tenant")
            .first()
        )
    if account is not None:
        return account, account.tenant

    username = os.getenv("WHATSAPP_TENANT", "").strip()
    if username:
        tenant = get_user_model().objects.filter(username=username).first()
        if tenant is not None:
            return None, tenant
    return None, None


WELCOME_MESSAGE = (
    "👋 Welcome! I'm your assistant. Send a question and I'll do my best to "
    "help.\n\nCommands:\n  /help — show this message\n  /lang <code> — set "
    "language preference (e.g. /lang en)"
)
# Sent when the tenant can't be served (out of points, plan lapsed, suspended,
# rate-limited): the customer is not told about the business's billing.
UNAVAILABLE_MESSAGE = "This assistant is unavailable right now. Please try again later."


def get_or_create_user(*, phone_number: str, profile_name: str = "", account=None) -> WhatsAppUser:
    """Find or open the contact record for ``phone_number`` ON ``account``.

    Keyed by (account, phone_number), not by phone number alone: one person can
    write to two different tenants' bots, and they must be two separate contacts
    with separate sessions and history. Sharing one row let each tenant see the
    other's conversation with that person.
    """
    user, created = WhatsAppUser.objects.get_or_create(
        account=account,
        phone_number=phone_number,
        defaults={"profile_name": profile_name},
    )
    if not created and profile_name and user.profile_name != profile_name:
        user.profile_name = profile_name
    if not created and account is not None and user.account_id is None:
        # A row from before the column existed, now attributable.
        user.account = account
        user.save(update_fields=["account", "updated_at"])
    user.update_last_message_time()
    return user


def get_or_create_session(user: WhatsAppUser) -> WhatsAppSession:
    session = WhatsAppSession.objects.filter(user=user, is_active=True).order_by("-updated_at").first()
    if session is None or session.is_expired():
        session = WhatsAppSession.objects.create(
            user=user,
            session_type=WhatsAppSession.SessionType.RAG_CHAT,
            expires_at=timezone.now() + timezone.timedelta(hours=24),
        )
    else:
        session.extend_session()
    return session


def log_message(
    *,
    user: WhatsAppUser,
    session: WhatsAppSession | None,
    message_type: str,
    text: str,
    whatsapp_message_id: str = "",
    response_time_ms: int | None = None,
    error_message: str = "",
    status: str = WhatsAppMessage.MessageStatus.SENT,
) -> WhatsAppMessage:
    return WhatsAppMessage.objects.create(
        user=user,
        session=session,
        message_type=message_type,
        message_text=text,
        whatsapp_message_id=whatsapp_message_id or None,
        response_time_ms=response_time_ms,
        error_message=error_message,
        status=status,
    )


def mark_delivery(message: WhatsAppMessage, *, sent_id: str = "", error: str = "") -> None:
    """Stamp a logged reply with Meta's message id, or with why it wasn't sent."""
    if error:
        message.status = WhatsAppMessage.MessageStatus.FAILED
        message.error_message = (f"{message.error_message}\n" if message.error_message else "") + error
    message.whatsapp_message_id = sent_id or message.whatsapp_message_id
    message.save(update_fields=["status", "error_message", "whatsapp_message_id"])


@dataclass
class IncomingResult:
    """What handling one incoming message produced. ``outgoing`` is the logged
    reply; the caller stamps it with Meta's message id (or the send failure)."""

    reply: str
    user: WhatsAppUser
    session: WhatsAppSession
    account: WhatsAppAccount | None
    outgoing: WhatsAppMessage


def _claim(*, user, session, text: str, message_id: str) -> WhatsAppMessage | None:
    """Log the incoming message, or return None when Meta already delivered it.

    Meta re-delivers a message whenever it isn't sure we got it. The database
    refuses a second incoming row with the same Meta id (a partial unique
    constraint), so of two concurrent deliveries exactly one is answered and
    charged.
    """
    try:
        with transaction.atomic():
            return log_message(
                user=user, session=session, message_type="incoming", text=text, whatsapp_message_id=message_id
            )
    except IntegrityError:
        return None


def conversation_history(session: WhatsAppSession, *, before, limit=None, minutes=None) -> list[dict]:
    """The recent turns of this conversation, oldest first, for the answer's
    context (so "and how much is it?" knows what "it" is). Only messages from
    the last WHATSAPP_HISTORY_MINUTES count, up to WHATSAPP_HISTORY_MESSAGES,
    unless the caller asks for a different window."""
    limit = int(limit if limit is not None else getattr(settings, "WHATSAPP_HISTORY_MESSAGES", 10))
    minutes = int(minutes if minutes is not None else getattr(settings, "WHATSAPP_HISTORY_MINUTES", 60))
    if limit <= 0 or session is None:
        return []
    rows = (
        WhatsAppMessage.objects.filter(
            session=session,
            created_at__lt=before,
            created_at__gte=timezone.now() - timezone.timedelta(minutes=minutes),
        )
        .exclude(message_text="")
        .order_by("-created_at")[:limit]
    )
    roles = {WhatsAppMessage.MessageType.INCOMING: "user", WhatsAppMessage.MessageType.OUTGOING: "assistant"}
    return [{"role": roles[m.message_type], "content": m.message_text} for m in reversed(rows)]


def handle_incoming_message(message_data: dict) -> IncomingResult | None:
    """Process an incoming text message; None when it is a duplicate delivery.

    The reply is generated from the knowledge of the TENANT that owns the
    business number that received the message (multi-tenant), with the recent
    turns of the conversation as context. Persists the incoming and outgoing
    messages and updates session/user state.
    """
    text = message_data["message_text"]
    # Resolve the receiving business number first: it is the tenant boundary,
    # and the contact row has to be created inside it.
    account, tenant = resolve_account_and_tenant(message_data.get("phone_number_id", ""))

    user = get_or_create_user(
        phone_number=message_data["from_number"],
        profile_name=message_data.get("profile_name", ""),
        account=account,
    )
    session = get_or_create_session(user)

    incoming = _claim(user=user, session=session, text=text, message_id=message_data.get("message_id") or "")
    if incoming is None:
        logger.info("WhatsApp: ignoring a re-delivery of %s", message_data.get("message_id"))
        return None
    user.increment_message_count()
    session.increment_message_count()
    # A restaurant with a menu takes orders here; everyone else gets RAG answers.
    ordering = chat_ordering.ordering_agent(tenant) if tenant is not None else None
    window = chat_ordering.HISTORY if ordering is not None else {}
    history = conversation_history(session, before=incoming.created_at, **window)

    started = time.monotonic()
    try:
        reply = _route(text, user, tenant, history=history, session=session, ordering=ordering)
        error_message = ""
    except Exception as exc:  # noqa: BLE001 — surfaced to operator via logs
        logger.exception("Error generating WhatsApp reply")
        reply = "Sorry, something went wrong. Please try again."
        error_message = str(exc)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    outgoing = log_message(
        user=user,
        session=session,
        message_type="outgoing",
        text=reply,
        response_time_ms=elapsed_ms,
        error_message=error_message,
    )
    return IncomingResult(reply=reply, user=user, session=session, account=account, outgoing=outgoing)


def _route(
    text: str,
    user: WhatsAppUser,
    tenant,
    *,
    history: list[dict] | None = None,
    session: WhatsAppSession | None = None,
    ordering=None,
) -> str:
    lowered = text.lower().strip()
    if lowered in {"/start", "/help", "start", "help"}:
        return WELCOME_MESSAGE
    if lowered.startswith("/lang"):
        return _handle_language(text, user)

    if tenant is None:
        # This business number isn't linked to any tenant's knowledge.
        return "This WhatsApp number isn't connected to an assistant yet. Please contact support."

    # The same gate as the chat API: the answer is paid from the tenant's points.
    from knowledge.models import UsageKind
    from knowledge.services import billing, quota

    try:
        quota.check_chat_allowed(tenant)
    except quota.QuotaError as exc:
        logger.info("WhatsApp reply refused for tenant %s: %s", tenant.pk, exc.message)
        return UNAVAILABLE_MESSAGE
    reserved = quota.reserve_chat_points(tenant)
    if reserved is None:
        return UNAVAILABLE_MESSAGE

    started = time.monotonic()
    if ordering is not None and session is not None:
        with billing.charge_to(tenant, UsageKind.CHAT, reserved=reserved) as scope:
            reply = chat_ordering.reply(
                agent=ordering, session=session, customer_phone=user.phone_number, text=text, history=history or []
            )
            scope.record.update(response_time_ms=int((time.monotonic() - started) * 1000))
        return reply
    try:
        with billing.charge_to(tenant, UsageKind.CHAT, reserved=reserved) as scope:
            result = answer_question(text, history=history or [], user=tenant, language=detect_language(text))
            scope.record.update(
                response_time_ms=int((time.monotonic() - started) * 1000),
                confident=result.confident,
                chunk_count=len(result.sources),
            )
    except RagUnavailable:
        return "I cannot search our knowledge base right now. Please try again later."
    return result.answer


def _handle_language(text: str, user: WhatsAppUser) -> str:
    parts = text.split()
    if len(parts) < 2:
        choices = ", ".join(c.value for c in WhatsAppUser.LanguagePreference)
        return f"Available languages: {choices}\nUsage: /lang en"
    code = parts[1].strip().lower()
    valid = {c.value for c in WhatsAppUser.LanguagePreference}
    if code not in valid:
        return f"Unsupported language: {code}"
    user.language_preference = code
    user.save(update_fields=["language_preference", "updated_at"])
    return f"Language set to {code}."
