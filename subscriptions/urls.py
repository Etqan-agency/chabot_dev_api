from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    FeatureCatalogView,
    MyFeaturesView,
    MySubscriptionView,
    PaddleWebhookView,
    PlanViewSet,
    PointsHistoryView,
    SubscriptionViewSet,
    UserFeaturesView,
)

router = DefaultRouter()
router.register("plans", PlanViewSet, basename="plan")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")

urlpatterns = [
    path("my-subscription/", MySubscriptionView.as_view(), name="my-subscription"),
    path("points-history/", PointsHistoryView.as_view(), name="points-history"),
    # Feature access control (per-user, admin-managed from the dashboard).
    path("feature-catalog/", FeatureCatalogView.as_view(), name="feature-catalog"),
    path("my-features/", MyFeaturesView.as_view(), name="my-features"),
    path("user-features/<str:user_id>/", UserFeaturesView.as_view(), name="user-features"),
    # Public: Paddle Billing notifications (payment fulfillment).
    path("billing/paddle/webhook/", PaddleWebhookView.as_view(), name="paddle-webhook"),
    path("", include(router.urls)),
]
