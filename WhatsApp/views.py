import json
import logging
import threading

from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    WhatsAppAccount,
    WhatsAppAnalytics,
    WhatsAppMessage,
    WhatsAppSession,
    WhatsAppUser,
)
from .serializers import (
    EmbeddedSignupSerializer,
    WhatsAppAccountSerializer,
    WhatsAppAnalyticsSerializer,
    WhatsAppConnectionAccountSerializer,
    WhatsAppMessageSerializer,
    WhatsAppSendResponseSerializer,
    WhatsAppSendSerializer,
    WhatsAppSessionSerializer,
    WhatsAppUserSerializer,
)
from .services import embedded_signup, webhook_auth
from .services.conversation import (
    handle_incoming_message,
    log_message,
    mark_delivery,
)
from .services.meta_client import (
    MetaWhatsAppClient,
    WhatsAppClientError,
    WhatsAppConfig,
    parse_incoming_message,
    parse_status_updates,
)

logger = logging.getLogger(__name__)


def process_incoming(message_data: dict) -> None:
    """Answer one incoming message and send the reply FROM the number that
    received it (per tenant). A re-delivery of a message already answered is
    dropped by ``handle_incoming_message``. Never raises."""
    try:
        result = handle_incoming_message(message_data)
    except Exception:  # noqa: BLE001
        logger.exception("Error processing incoming WhatsApp message")
        return
    if result is None:
        return  # duplicate delivery: already answered

    client = MetaWhatsAppClient(WhatsAppConfig.for_account(result.account) if result.account else None)
    try:
        sent_id = client.send_text(message_data["from_number"], result.reply)
    except WhatsAppClientError as exc:
        logger.error("Could not deliver reply to WhatsApp: %s", exc)
        mark_delivery(result.outgoing, error=str(exc))
        return
    mark_delivery(result.outgoing, sent_id=sent_id)


def _run_in_thread(message_data: dict) -> None:
    from django.db import connection

    try:
        process_incoming(message_data)
    finally:
        connection.close()  # each thread has its own connection


def dispatch_incoming(message_data: dict) -> None:
    """Answer after the webhook has returned 200 (``WHATSAPP_PROCESS_MODE``):
    an LLM answer takes seconds, and Meta re-delivers anything it waited on."""
    if getattr(settings, "WHATSAPP_PROCESS_MODE", "thread") == "sync":
        process_incoming(message_data)
        return
    threading.Thread(target=_run_in_thread, args=(message_data,), daemon=True).start()


@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(APIView):
    """Receive webhook events from the Meta WhatsApp Cloud API."""

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.client = MetaWhatsAppClient()

    @extend_schema(
        parameters=[
            OpenApiParameter("hub.mode", str),
            OpenApiParameter("hub.verify_token", str),
            OpenApiParameter("hub.challenge", str),
        ],
        responses={200: str, 403: str},
    )
    def get(self, request):
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge", "")
        if self.client.verify_webhook(mode, token):
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("Verification failed", status=403)

    def post(self, request):
        # Prove this came from Meta BEFORE anything reads the body. Everything
        # downstream -- answering, spending the tenant's credits, sending from
        # their number -- is an action taken on the payload's say-so.
        allowed, reason = webhook_auth.check(request.body, request.headers.get(webhook_auth.HEADER))
        if not allowed:
            logger.warning("WhatsApp webhook: refused a delivery (%s)", reason)
            # Deliberately terse: the caller learns nothing about why.
            return HttpResponse("Forbidden", status=403)

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)

        # Status updates and incoming messages share the same envelope.
        for status_update in parse_status_updates(payload):
            logger.info(
                "WhatsApp status update: %s -> %s",
                status_update.get("id"),
                status_update.get("status"),
            )

        message_data = parse_incoming_message(payload)
        if message_data is None or not message_data["message_text"]:
            return HttpResponse("OK")

        # Acknowledge now, answer in the background: Meta re-delivers a message
        # it had to wait on, and the answer (and its charge) must happen once.
        dispatch_incoming(message_data)
        return HttpResponse("OK")


class WhatsAppSendView(APIView):
    """Send a free-form text message to a WhatsApp user."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=WhatsAppSendSerializer,
        responses={200: WhatsAppSendResponseSerializer},
    )
    def post(self, request):
        try:
            from subscriptions.features import has_feature

            allowed = has_feature(request.user, "whatsapp")
        except Exception:
            allowed = True  # fail open on a billing-layer hiccup
        if not allowed:
            return Response(
                {"detail": "WhatsApp sending isn't enabled for your account."},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        serializer = WhatsAppSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Send FROM the caller's own business number, never the platform's.
        # Staff may name another tenant's account.
        accounts = WhatsAppAccount.objects.filter(is_active=True)
        if data.get("account"):
            if not request.user.is_staff:
                return Response(
                    {"detail": "Only admins can send from another account's number."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            account = accounts.filter(pk=data["account"]).first()
        else:
            account = accounts.filter(tenant=request.user).first()
        if account is None:
            return Response(
                {"detail": "No active WhatsApp number is connected to this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = MetaWhatsAppClient(WhatsAppConfig.for_account(account))
        try:
            message_id = client.send_text(data["to_number"], data["message"])
        except WhatsAppClientError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Keep the conversation complete when the recipient is a known contact.
        contact = WhatsAppUser.objects.filter(account=account, phone_number=data["to_number"]).first()
        if contact is not None:
            session = contact.sessions.filter(is_active=True).order_by("-updated_at").first()
            log_message(
                user=contact,
                session=session,
                message_type="outgoing",
                text=data["message"],
                whatsapp_message_id=message_id,
            )
        return Response({"message_id": message_id, "status": "sent"})


def _whatsapp_allowed(user) -> bool:
    try:
        from subscriptions.features import has_feature

        return has_feature(user, "whatsapp")
    except Exception:
        return True  # fail open on a billing-layer hiccup


def _connection_payload(user) -> dict:
    account = WhatsAppAccount.objects.filter(tenant=user).first()
    config = embedded_signup.signup_config()
    return {
        "connected": bool(account and account.is_active),
        "account": WhatsAppConnectionAccountSerializer(account).data if account else None,
        "allowed": _whatsapp_allowed(user),
        # What the dashboard needs to open Meta's signup popup.
        "signup": {
            "enabled": config.enabled,
            "app_id": config.app_id,
            "config_id": config.config_id,
            "graph_version": config.graph_version,
        },
    }


class WhatsAppConnectionView(APIView):
    """The caller's own WhatsApp number: status, and what the dashboard needs
    to connect one with Meta's Embedded Signup."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: dict})
    def get(self, request):
        return Response(_connection_payload(request.user))


class WhatsAppEmbeddedSignupView(APIView):
    """Finish Embedded Signup: exchange Meta's code and connect the number."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=EmbeddedSignupSerializer, responses={200: dict})
    def post(self, request):
        if not _whatsapp_allowed(request.user):
            return Response(
                {"detail": "WhatsApp isn't enabled for your account.", "code": "feature_disabled"},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        serializer = EmbeddedSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            _account, warning = embedded_signup.complete_signup(request.user, **serializer.validated_data)
        except embedded_signup.SignupError as exc:
            return Response({"detail": exc.detail, "code": exc.code}, status=exc.status)
        payload = _connection_payload(request.user)
        payload["warning"] = warning
        return Response(payload)


class WhatsAppDisconnectView(APIView):
    """Stop the bot on the caller's number. Conversations are kept."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(request=None, responses={200: dict})
    def post(self, request):
        account = WhatsAppAccount.objects.filter(tenant=request.user, is_active=True).first()
        if account is None:
            return Response(
                {"detail": "No WhatsApp number is connected to this account.", "code": "not_connected"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        embedded_signup.disconnect(account)
        return Response(_connection_payload(request.user))


class TenantScopedWhatsAppMixin:
    """Confine a WhatsApp queryset to the caller's own business number.

    These viewsets used to expose ``Model.objects.all()`` behind nothing but
    IsAuthenticated. Every tenant could therefore list every other tenant's
    WhatsApp contacts, sessions, analytics and -- through a searchable
    ``message_text`` field -- the full text of their customer conversations.

    Scoping happens here, on the queryset, rather than in each view: a filter
    that must be repeated in four places is a filter that will be forgotten in
    the fifth. Staff keep the unscoped view (support and the legacy rows that
    predate the account column, which cannot be attributed after the fact).
    """

    #: Lookup from this model to WhatsAppUser.account, as a queryset filter.
    tenant_lookup = "account"

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if getattr(user, "is_staff", False):
            return qs
        account = WhatsAppAccount.objects.filter(tenant=user).first()
        if account is None:
            # No business number linked to this tenant -> no WhatsApp data.
            return qs.none()
        return qs.filter(**{self.tenant_lookup: account})


class WhatsAppUserViewSet(TenantScopedWhatsAppMixin, viewsets.ReadOnlyModelViewSet):
    queryset = WhatsAppUser.objects.all()
    tenant_lookup = "account"
    serializer_class = WhatsAppUserSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "language_preference"]
    search_fields = ["phone_number", "profile_name"]
    ordering_fields = ["created_at", "last_message_at", "message_count"]
    ordering = ["-last_message_at"]


class WhatsAppSessionViewSet(TenantScopedWhatsAppMixin, viewsets.ReadOnlyModelViewSet):
    queryset = WhatsAppSession.objects.all()
    tenant_lookup = "user__account"
    serializer_class = WhatsAppSessionSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["session_type", "is_active", "user"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-updated_at"]


class WhatsAppMessageViewSet(TenantScopedWhatsAppMixin, viewsets.ReadOnlyModelViewSet):
    queryset = WhatsAppMessage.objects.all()
    tenant_lookup = "user__account"
    serializer_class = WhatsAppMessageSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["message_type", "status", "user", "session"]
    search_fields = ["message_text"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]


class WhatsAppAnalyticsViewSet(viewsets.ReadOnlyModelViewSet):
    """Platform-wide WhatsApp volume, aggregated per DAY across all tenants.

    There is no tenant column to scope by -- a row is one day's totals for the
    whole installation -- so this cannot be shown to a tenant without telling
    them how much traffic every other tenant handles. Staff only, until the
    model carries an account.
    """

    queryset = WhatsAppAnalytics.objects.all()
    serializer_class = WhatsAppAnalyticsSerializer
    permission_classes = [permissions.IsAdminUser]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["date"]
    ordering_fields = ["date"]
    ordering = ["-date"]


class WhatsAppAccountViewSet(viewsets.ModelViewSet):
    """Admin: link a tenant to a WhatsApp Business number (multi-tenant routing).

    Create one per tenant: which `phone_number_id` belongs to which user, and
    optionally a per-number send token. The webhook then answers from that
    tenant's knowledge and replies from that number.
    """

    queryset = WhatsAppAccount.objects.select_related("tenant").all()
    serializer_class = WhatsAppAccountSerializer
    permission_classes = [permissions.IsAdminUser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["is_active", "tenant"]
    search_fields = ["phone_number_id", "display_name", "tenant__username"]
    ordering_fields = ["created_at", "display_name"]
    ordering = ["-created_at"]
