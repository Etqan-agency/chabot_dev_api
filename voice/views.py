"""Voice ordering API.

Three audiences:

* ``tools/*`` -- called by the hosted voice agent mid-call (tool token auth).
* ``webhooks/elevenlabs/`` -- the post-call report (HMAC-signed).
* everything else -- the tenant's dashboard (JWT), always scoped to the caller.

Tool endpoints answer business problems ("that item isn't on the menu") with
200 and ``{"ok": false, "error": ...}`` so the agent reads the reason and asks
the caller, instead of treating it as a broken tool.
"""

import json
import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from . import auth
from .models import MenuItem, Order, VoiceAgent, VoiceCall
from .serializers import (
    CreateOrderToolSerializer,
    KnowledgeToolSerializer,
    MenuItemSerializer,
    OrderSerializer,
    QuoteOrderToolSerializer,
    VoiceAgentSerializer,
    VoiceCallListSerializer,
    VoiceCallSerializer,
)
from .services import calls as call_service
from .services import tools
from .services.orders import LineRequest, OrderError, place_order, quote_order

logger = logging.getLogger(__name__)


# -- Agent tools --------------------------------------------------------------- #


class _ToolView(APIView):
    authentication_classes = [auth.VoiceToolAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "chat"


class MenuToolView(_ToolView):
    """The available menu, for the agent to quote names and prices from."""

    def get(self, request):
        return Response(tools.menu_payload(request.auth))


class KnowledgeToolView(_ToolView):
    """Raw passages from the tenant's knowledge base; the agent phrases the answer."""

    def post(self, request):
        ser = KnowledgeToolSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(tools.search_knowledge(request.user, ser.validated_data["query"]))


class QuoteOrderToolView(_ToolView):
    """Price an order without placing it, so the agent reads back the real total."""

    def post(self, request):
        ser = QuoteOrderToolSerializer(data=request.data)
        if not ser.is_valid():
            return Response({"ok": False, "error": ser.errors})
        data = ser.validated_data
        try:
            quote = quote_order(
                request.auth,
                lines=[LineRequest(**line) for line in data["items"]],
                fulfilment=data["fulfilment"],
            )
        except OrderError as exc:
            return Response({"ok": False, "error": str(exc)})
        return Response(
            {
                "ok": True,
                "items": [
                    {"name": item.name, "quantity": line.quantity, "notes": line.notes, "line_total": str(total)}
                    for item, line, total in quote.priced
                ],
                "subtotal": str(quote.subtotal),
                "delivery_fee": str(quote.delivery_fee),
                "total": str(quote.total),
                "currency": request.auth.currency,
            }
        )


class CreateOrderToolView(_ToolView):
    """Place the order the caller confirmed. Prices and totals come from the DB."""

    def post(self, request):
        ser = CreateOrderToolSerializer(data=request.data)
        if not ser.is_valid():
            return Response({"ok": False, "error": ser.errors})
        data = ser.validated_data
        try:
            order, created = place_order(
                request.auth,
                lines=[LineRequest(**line) for line in data["items"]],
                fulfilment=data["fulfilment"],
                customer_name=data["customer_name"],
                customer_phone=data["customer_phone"],
                address=data["address"],
                notes=data["notes"],
                conversation_id=data["conversation_id"],
            )
        except OrderError as exc:
            return Response({"ok": False, "error": str(exc)})
        return Response(
            {
                "ok": True,
                "created": created,
                "order_number": order.number,
                "items": [
                    {"name": i.name, "quantity": i.quantity, "line_total": str(i.line_total)}
                    for i in order.items.all()
                ],
                "subtotal": str(order.subtotal),
                "delivery_fee": str(order.delivery_fee),
                "total": str(order.total),
                "currency": order.currency,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# -- Post-call webhook ----------------------------------------------------------- #


@method_decorator(csrf_exempt, name="dispatch")
class ElevenLabsWebhookView(View):
    def post(self, request):
        raw = request.body
        if not auth.verify_signature(raw, request.headers.get(auth.SIGNATURE_HEADER)):
            return JsonResponse({"detail": "Invalid signature."}, status=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            return JsonResponse({"detail": "Invalid JSON."}, status=400)
        call_service.record_post_call(payload)
        # 200 for events we skip too, so the provider doesn't retry them.
        return JsonResponse({"ok": True})


# -- Dashboard ------------------------------------------------------------------ #


class VoiceAgentView(APIView):
    """GET/PATCH the caller's voice settings; POST issues a new tool token."""

    def _agent(self, request):
        agent, _ = VoiceAgent.objects.get_or_create(tenant=request.user)
        return agent

    def get(self, request):
        return Response(VoiceAgentSerializer(self._agent(request)).data)

    def patch(self, request):
        ser = VoiceAgentSerializer(self._agent(request), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def post(self, request):
        """Issue (or rotate) the tool token. The plaintext is returned only here."""
        agent = self._agent(request)
        token = agent.issue_token()
        return Response({**VoiceAgentSerializer(agent).data, "token": token}, status=status.HTTP_201_CREATED)


class _TenantScoped:
    def get_queryset(self):
        return super().get_queryset().filter(tenant=self.request.user)


class MenuItemViewSet(_TenantScoped, viewsets.ModelViewSet):
    queryset = MenuItem.objects.all()
    serializer_class = MenuItemSerializer
    filterset_fields = ["category", "is_available"]
    search_fields = ["name", "category"]
    ordering_fields = ["name", "price", "sort_order", "created_at"]
    pagination_class = None  # a menu is small; the dashboard edits it as one list

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user)


class OrderViewSet(
    _TenantScoped,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Orders list/detail; the dashboard may only move ``status``."""

    queryset = Order.objects.prefetch_related("items", "calls")
    serializer_class = OrderSerializer
    filterset_fields = ["status", "fulfilment", "source"]
    search_fields = ["customer_name", "customer_phone", "address"]
    ordering_fields = ["created_at", "number", "total"]
    http_method_names = ["get", "patch", "head", "options"]


class VoiceCallViewSet(_TenantScoped, viewsets.ReadOnlyModelViewSet):
    queryset = VoiceCall.objects.select_related("order")
    filterset_fields = ["status"]
    search_fields = ["caller_number", "summary"]
    ordering_fields = ["created_at", "duration_secs"]

    def get_serializer_class(self):
        return VoiceCallSerializer if self.action == "retrieve" else VoiceCallListSerializer
