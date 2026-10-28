from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    APIIngestionRunView,
    AvailableLanguageViewSet,
    ChatAPIView,
    ChatbotConfigView,
    ChatFeedbackAPIView,
    CrawlWebsiteView,
    GuidedTreeViewSet,
    KnowledgeContentViewSet,
    SyncAPIContentView,
    UnansweredQuestionViewSet,
    UploadedDocumentViewSet,
    UsageAnalyticsView,
)

router = DefaultRouter()
router.register("documents", UploadedDocumentViewSet, basename="document")
router.register("knowledge", KnowledgeContentViewSet, basename="knowledge")
router.register("unanswered", UnansweredQuestionViewSet, basename="unanswered")
router.register("guided-tree", GuidedTreeViewSet, basename="guided-tree")
router.register("tree-languages", AvailableLanguageViewSet, basename="tree-language")

urlpatterns = [
    path("chat/", ChatAPIView.as_view(), name="chat"),
    path("chat/feedback/", ChatFeedbackAPIView.as_view(), name="chat-feedback"),
    path("chatbot-config/", ChatbotConfigView.as_view(), name="chatbot-config"),
    path("sync-api-content/", SyncAPIContentView.as_view(), name="sync-api-content"),
    path(
        "sync-api-content/runs/<uuid:run_id>/",
        APIIngestionRunView.as_view(),
        name="api-ingestion-run",
    ),
    path("crawl-website/", CrawlWebsiteView.as_view(), name="crawl-website"),
    path("analytics/usage/", UsageAnalyticsView.as_view(), name="analytics-usage"),
    path("", include(router.urls)),
]
