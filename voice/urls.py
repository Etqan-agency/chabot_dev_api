from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CreateOrderToolView,
    ElevenLabsWebhookView,
    KnowledgeToolView,
    MenuItemViewSet,
    MenuToolView,
    OrderViewSet,
    QuoteOrderToolView,
    VoiceAgentView,
    VoiceCallViewSet,
)

router = DefaultRouter()
router.register("menu-items", MenuItemViewSet, basename="voice-menu-item")
router.register("orders", OrderViewSet, basename="voice-order")
router.register("calls", VoiceCallViewSet, basename="voice-call")

urlpatterns = [
    path("tools/menu/", MenuToolView.as_view(), name="voice-tool-menu"),
    path("tools/knowledge/", KnowledgeToolView.as_view(), name="voice-tool-knowledge"),
    path("tools/quote/", QuoteOrderToolView.as_view(), name="voice-tool-quote"),
    path("tools/orders/", CreateOrderToolView.as_view(), name="voice-tool-orders"),
    path("webhooks/elevenlabs/", ElevenLabsWebhookView.as_view(), name="voice-webhook-elevenlabs"),
    path("agent/", VoiceAgentView.as_view(), name="voice-agent"),
    path("", include(router.urls)),
]
